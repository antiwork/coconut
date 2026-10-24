"""Write the settings that a bot inherited into its own profile, once, with a backup and a report.

Before this step, each bot inherited the MCP servers, the approval settings, the API fallback and the model of Coconut's
default Hermes home, and Coconut pointed CODEX_HOME, GH_CONFIG_DIR and XDG_* into the profile. Now each profile uses its
own settings, as in Hermes. This step writes those settings into the bot's own `config.yaml` and `.env`, so the bot
works as before.

The step also gives a new bot a copy of the default settings, as `hermes profile create --clone` does.

Each bot gets the folder `<data>/bots/<uuid>/settings-migration/`:
- `backup/` holds `config.yaml`, `.env` and the Coconut files from the profile, as they were before the step.
- `report.json` says what changed. It marks the step as done, so a later start skips the bot.

Each part checks the profile before it writes, so a step that stopped halfway can run again safely. This module runs
in the native settings process, under the Python of the installed Hermes.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from native_settings import mode, raw_config, save_config
from profiles import _write, settings_report

VERSION = 1
# Coconut pointed these variables into the profile. A folder that holds files keeps its variable in the profile .env.
REDIRECTS = (('CODEX_HOME', '.codex'), ('GH_CONFIG_DIR', '.config/gh'),
             ('XDG_CONFIG_HOME', '.config'), ('XDG_DATA_HOME', '.local/share'))
# Coconut files that earlier versions kept in the profile. The step moves them into the backup.
COCONUT_FILES = ('coconut-profile.json', '.coconut-kanban-tools', '.coconut-native-config.json',
                 'coconut-terminal.sh', 'coconut-google.sh')
SHELL_SCRIPTS = ('coconut-terminal.sh', 'coconut-google.sh')
BACKUP_COMPLETE = '.complete'
# A copied fallback whose checks have not passed yet. Each attempt repeats them until the bot is done.
PENDING = 'pending.json'
ATTEMPT = 'attempt'
# The step writes only the OpenAI API fallback, which was the only fallback that bots inherited.
FALLBACK_KEYS = ('fallback_providers', 'fallback_model')


def _copy(source, target):
    """Copy the bytes and the mode of one file atomically."""
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.coconut-', delete=False) as handle:
        handle.write(source.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    shutil.copymode(source, handle.name)
    os.replace(handle.name, target)


def _back_up(home, backup):
    """Copy the profile files into the backup once, before the step changes anything.

    The `.complete` marker closes the backup. A run after a stop keeps the first backup, which shows the state before
    the step, also for a file that the step created later.
    """
    if (backup / BACKUP_COMPLETE).exists():
        return
    for name in ('config.yaml', '.env', *COCONUT_FILES):
        source, target = home / name, backup / name
        if source.is_file() and not source.is_symlink() and not target.exists():
            _copy(source, target)
    _write(backup / BACKUP_COMPLETE, '', backup)


def _env(home):
    from agent.secret_scope import load_env_file
    return load_env_file(Path(home) / '.env')


def _save_env(key, value):
    """Write one variable into the .env file of the active profile home. Raise when Hermes refuses it."""
    from hermes_cli.config import require_env_writable, save_env_value
    require_env_writable(key, 'set')
    save_env_value(key, value)


def _legacy_defaults(home, config, backup):
    """Apply the profile defaults of earlier Coconut versions that this profile has not received yet.

    The step moves the markers of those defaults into the backup, so a run after a stop finds them there.
    """
    def marked(name):
        return (home / name).exists() or (backup / name).exists()
    changed = False
    if not marked('.coconut-kanban-tools'):
        toolsets = list(dict.fromkeys(list(config.get('toolsets') or []) + ['kanban']))
        if config.get('toolsets') != toolsets:
            config['toolsets'] = toolsets
            changed = True
    delegation = config.get('delegation')
    if not isinstance(delegation, dict):
        delegation = {}
    if 'max_concurrent_children' not in delegation or 'worktree_isolation' not in delegation:
        # Null selects Hermes's environment fallback, which carries the worker limit of the Coconut settings.
        delegation.setdefault('max_concurrent_children', None)
        delegation.setdefault('worktree_isolation', True)
        config['delegation'] = delegation
        changed = True
    if marked('.coconut-native-config.json'):
        return changed
    terminal = config.get('terminal')
    if not isinstance(terminal, dict):
        terminal = {}
    if 'cwd' not in terminal:
        terminal['cwd'] = str(home.parent / 'workspace')
        config['terminal'] = terminal
        changed = True
    tools = config.get('tools') or {}
    if isinstance(tools, dict) and isinstance(tools.get('tool_search'), dict) and tools['tool_search'].get('enabled') == 'off':
        tools['tool_search'].pop('enabled', None)
        changed = True
    agent = config.get('agent') or {}
    if isinstance(agent, dict) and agent.get('environment_probe') is False:
        agent.pop('environment_probe', None)
        changed = True
    # Native memory limits must fit all entries imported from the former store.
    for filename, key in (('MEMORY.md', 'memory_char_limit'), ('USER.md', 'user_char_limit')):
        path = home / 'memories' / filename
        if path.is_file():
            size = len(path.read_text())
            memory = config.get('memory') if isinstance(config.get('memory'), dict) else {}
            if size > 1000 and memory.get(key, 0) < size + 1000:
                memory[key] = size + 1000
                config['memory'] = memory
                changed = True
    return changed


def _approvals(config, default_config):
    """Copy each approval setting of the default home that the profile does not set. Hermes merged them before."""
    inherited = default_config.get('approvals') or {}
    if not isinstance(inherited, dict) or not inherited:
        return {}
    approvals = config.get('approvals')
    if not isinstance(approvals, dict):
        approvals = {}
    copied = {}
    for key, value in inherited.items():
        if key not in approvals:
            approvals[key] = mode(value) if key == 'mode' else deepcopy(value)
            copied[key] = approvals[key]
    if copied:
        config['approvals'] = approvals
    return copied


def _model_is_set(config):
    model = config.get('model')
    if isinstance(model, str):
        return bool(model.strip())
    return isinstance(model, dict) and any(model.get(key) for key in ('default', 'model', 'name'))


def _signed_in(homes, provider):
    """Whether the default homes keep a sign-in for this provider. A profile outside the Hermes profiles folder cannot
    use that sign-in, and a copy of it would sign the default home out when one of them refreshes it."""
    from model_catalog import signed_in
    return any(signed_in(home, provider) for home in homes)


def _route(home, provider, model, overlays):
    """Resolve the model route as a turn does, with these .env files applied over the process environment."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from model_catalog import apply_env_file
    saved = dict(os.environ)
    token = set_hermes_home_override(str(home))
    try:
        for path in overlays:
            apply_env_file(path)
        runtime = resolve_runtime_provider(requested=provider, target_model=model)
    finally:
        reset_hermes_home_override(token)
        os.environ.clear()
        os.environ.update(saved)
    if callable(runtime.get('api_key')):
        raise ValueError('The route uses a credential function.')
    # The copied model block carries the endpoint. The credential is what the profile can change.
    return runtime.get('provider'), runtime.get('api_key')


def _same_route(default, home, provider, model):
    """Whether the bot's own profile resolves the same provider and key as the default home.

    A bot without its own model resolves its route in the default home. With the model in its profile, Hermes uses the
    profile's credentials. The step copies the model only when that changes nothing.
    """
    try:
        return (_route(default, provider, model, [default / '.env'])
                == _route(home, provider, model, [default / '.env', home / '.env']))
    except Exception:
        return False


def _model(config, default_config, default, home, root):
    if _model_is_set(config):
        own = config['model'] if isinstance(config.get('model'), dict) else {}
        provider = own.get('provider') or config.get('provider')
        from model_catalog import sign_in_home
        if provider and sign_in_home(provider, home, dict.fromkeys((default, root))) != home:
            # model_catalog resolves this route in the default home, where Hermes refreshes the sign-in.
            return {'status': 'kept_uses_default_sign_in', 'provider': provider}
        return {'status': 'kept'}
    model = default_config.get('model')
    if isinstance(model, str):
        model = {'default': model}
    name = model.get('default') or model.get('model') or model.get('name') if isinstance(model, dict) else None
    if not name:
        return {'status': 'no_default'}
    provider = model.get('provider') or default_config.get('provider')
    if provider and _signed_in(dict.fromkeys((default, root)), provider):
        # The bot keeps the default model. Hermes reads it from the default home, where the sign-in is.
        return {'status': 'uses_default_sign_in', 'provider': provider}
    if not provider or not _same_route(default, home, provider, name):
        # The bot keeps the default model, so its credentials stay the same.
        return {'status': 'uses_default_credentials', 'provider': provider}
    config['model'] = deepcopy(model)
    effort = (default_config.get('agent') or {}).get('reasoning_effort')
    agent = config.get('agent') if isinstance(config.get('agent'), dict) else {}
    if effort is not None and 'reasoning_effort' not in agent:
        agent['reasoning_effort'] = effort
        config['agent'] = agent
    return {'status': 'copied', 'model': name, 'provider': provider}


class MigrationError(RuntimeError):
    """The step cannot keep a setting that the bot used. It restores the profile files and leaves the bot not done."""


KEY_SETTINGS = ('key_env', 'api_key_env')


def _rename_reference(value, name, target):
    """Point every reference to one variable at another, in the forms that Hermes expands: ${NAME}, ${ NAME } and
    ${env:NAME}, at any depth of the route, and in its key_env or api_key_env setting."""
    pattern = re.compile(r'\$\{\s*(?:env:\s*)?' + re.escape(name) + r'\s*\}')
    if isinstance(value, dict):
        return {key: target if key in KEY_SETTINGS and item == name else _rename_reference(item, name, target)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_rename_reference(item, name, target) for item in value]
    if isinstance(value, str):
        return pattern.sub('${' + target + '}', value)
    return value


def _fallback(config, default_config, values, default_values):
    """Copy the OpenAI API fallback of the default home. Return the routes, the variables to write and the report.

    Each route keeps the key that it used in the default home. When the profile has another value under the same
    name, the route reads the default value from a separate variable. When the default home reads the key through its
    secret sources, the profile gets those sources too.
    """
    if any(key in config for key in FALLBACK_KEYS):
        return [], {}, {}
    from native_fallback import chain
    from native_mcp import _references
    entries = [deepcopy(entry) for entry in chain(default_config)
               if isinstance(entry, dict) and entry.get('provider') == 'openai-api']
    if not entries:
        return [], {}, {}
    writes, renamed, unresolved = {}, {}, set()
    for index, entry in enumerate(entries):
        names = _references(entry, set())
        setting = next((entry[key] for key in KEY_SETTINGS if isinstance(entry.get(key), str) and entry[key]), None)
        if setting:
            names.add(setting)
        elif not entry.get('api_key'):
            # Without its own key setting, Hermes reads OPENAI_API_KEY. A named variable keeps a renamed key.
            entry['key_env'] = 'OPENAI_API_KEY'
            names.add('OPENAI_API_KEY')
        for name in sorted(names):
            value = default_values.get(name)
            if not value:
                unresolved.add(name)
                continue
            target = name
            if values.get(name) and values[name] != value:
                target = 'COCONUT_FALLBACK_' + name
                entry = entries[index] = _rename_reference(entry, name, target)
                renamed[name] = target
            if values.get(target) != value:
                writes[target] = value
    config['fallback_providers'] = entries
    secrets = False
    if unresolved and 'secrets' in default_config and 'secrets' not in config:
        config['secrets'] = deepcopy(default_config['secrets'])
        secrets = True
    return entries, writes, dict(providers=len(entries), renamed_variables=renamed,
                                 copied_secret_sources=secrets, variables_from_other_sources=sorted(unresolved))


def _fallback_keys(home):
    """The keys that the OpenAI API fallback routes of this home resolve to, in order. None for a route without one."""
    from native_fallback import chain, configuration, entry_key
    with configuration(home) as expanded:
        return [entry_key(entry) for entry in chain(expanded) if entry.get('provider') == 'openai-api']


def _secret_values(home):
    """The values that Hermes resolves for this profile: its .env with its secret sources over it."""
    from agent.secret_scope import current_secret_scope
    from native_fallback import configuration
    with configuration(home):
        return dict(current_secret_scope() or {})


def _snapshot(home, folder):
    """Copy the profile files and the pending record as they are just before this attempt.

    A failed attempt restores this snapshot, not the first backup, so changes that the person made after an earlier
    attempt stay. The first backup stays for recovery.
    """
    attempt = folder / ATTEMPT
    if attempt.exists():
        shutil.rmtree(attempt)
    attempt.mkdir(mode=0o700)
    for source in (home / 'config.yaml', home / '.env', folder / PENDING):
        if source.is_file() and not source.is_symlink():
            _copy(source, attempt / source.name)


def _restore(home, folder):
    """Put back the snapshot of this attempt. A file that the snapshot does not hold did not exist before it."""
    attempt = folder / ATTEMPT
    for target in (home / 'config.yaml', home / '.env', folder / PENDING):
        source = attempt / target.name
        if source.is_file():
            _copy(source, target)
        else:
            target.unlink(missing_ok=True)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _referenced_names(config):
    """The variables that a configuration refers to: ${NAME} references and key settings at any depth, and the
    OPENAI_API_KEY that an OpenAI API route without its own key setting reads."""
    from native_mcp import _references
    found = _references(config, set())

    def settings(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in KEY_SETTINGS and isinstance(item, str) and item:
                    found.add(item)
                settings(item)
        elif isinstance(value, list):
            for item in value:
                settings(item)
    settings(config)
    routes = config.get('fallback_providers')
    for route in routes if isinstance(routes, list) else []:
        if (isinstance(route, dict) and route.get('provider') == 'openai-api' and not route.get('api_key')
                and not any(route.get(key) for key in KEY_SETTINGS)):
            found.add('OPENAI_API_KEY')
    return found


def _undo_pending(home, pending):
    """Undo what an unchecked fallback copy wrote, and keep every other setting as it is now. Return whether the copy
    is fully undone.

    The step removes a copied route only while it is unchanged. It removes a copied variable or the copied secret
    sources only while they hold what the copy wrote and no setting that stays refers to them. When a setting that
    stays still uses a copied value, the step keeps that value and the copy stays pending, so the bot is not done.
    """
    from hermes_cli.config import remove_env_value
    config = raw_config(home)
    before = deepcopy(config)
    copied = pending.get('routes') or []
    routes = config.get('fallback_providers')
    if isinstance(routes, list):
        kept = [route for route in routes if route not in copied]
        if kept:
            config['fallback_providers'] = kept
        else:
            config.pop('fallback_providers', None)
    referenced = _referenced_names(config)
    values = _env(home)
    complete, remove = True, []
    for name, digest in pending.get('written', {}).items():
        if values.get(name) is None or _digest(values[name]) != digest:
            # The variable is gone, or the person changed it: it is not the copy's value any more.
            continue
        if name in referenced:
            complete = False
        else:
            remove.append(name)
    if pending.get('secrets') is not None and config.get('secrets') == pending['secrets']:
        defined = {name for name, value in values.items() if value and name not in remove}
        if referenced - defined:
            # A setting that stays can read a variable from these sources.
            complete = False
        else:
            config.pop('secrets', None)
    if config != before:
        save_config(home, config)
    for name in remove:
        remove_env_value(name)
    return complete


def _check_fallback(default, home, pending, values):
    """Check a copied fallback: the bot must resolve the default key, and copied secret sources must change no key."""
    expected = _fallback_keys(default)
    actual = _fallback_keys(home)
    if any(key and key != got for key, got in zip(expected, actual)) or len(actual) != len(expected):
        raise MigrationError('The bot could not keep the key of its OpenAI API fallback.')
    if pending['fallback'].get('copied_secret_sources'):
        # The copied sources must not replace a value that the bot already had, such as its model key.
        resolved = _secret_values(home)
        written = set(pending.get('written', {}))
        if any(resolved.get(name) != value for name, value in values.items() if value and name not in written):
            raise MigrationError('The secret sources of the OpenAI API fallback would change other bot keys.')


def _shell_init(home, config):
    """Remove the Coconut scripts from the shell setup. Coconut now adds them only while its worker runs."""
    terminal = config.get('terminal')
    if not isinstance(terminal, dict) or not isinstance(terminal.get('shell_init_files'), list):
        return []
    scripts = {str(home / name) for name in SHELL_SCRIPTS}
    files = terminal['shell_init_files']
    kept = [item for item in files if item not in scripts]
    if kept == files:
        return []
    if kept:
        terminal['shell_init_files'] = kept
    else:
        terminal.pop('shell_init_files')
    return sorted(set(files) - set(kept))


def _has_files(folder):
    if not folder.is_dir() or folder.is_symlink():
        return False
    return any(files for _, _, files in os.walk(folder))


def _redirects(home, values):
    """Keep the login folders that a bot used, through its own .env. Other bots use the Mac account's folders."""
    kept = {}
    for name, relative in REDIRECTS:
        folder = home / relative
        if name in values or not _has_files(folder):
            continue
        _save_env(name, str(folder))
        values[name] = str(folder)
        kept[name] = str(folder)
    return kept


def _move_runs(home):
    """Move the routine receipts from the profile into `<data>/bots/<uuid>/runs`. Hold each receipt's lock to move it."""
    import fcntl
    from native_routine_receipts import runs_directory
    source = home / 'coconut-runs'
    if not source.is_dir() or source.is_symlink():
        return {'moved': 0, 'kept': []}
    target = runs_directory(home)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    moved, kept = 0, []
    for path in sorted(source.glob('*.json')):
        # The receipt functions lock a routine's receipts with a .lock file. At the service start no worker runs, so
        # the lock of the receipt's own name is enough to stop a late writer of an earlier service.
        with (source / (path.stem + '.lock')).open('a') as old, (target / (path.stem + '.lock')).open('a') as new:
            fcntl.flock(old, fcntl.LOCK_EX)
            fcntl.flock(new, fcntl.LOCK_EX)
            destination = target / path.name
            if destination.exists():
                # A newer receipt already exists. Keep both, and report the old one for review.
                kept.append(path.name)
                continue
            os.replace(path, destination)
            moved += 1
    if not kept:
        # Coconut leaves nothing in the profile. The locks in the new folder guard the receipts from now on.
        for lock in source.glob('*.lock'):
            lock.unlink()
        try:
            source.rmdir()
        except OSError:
            pass
    return {'moved': moved, 'kept': kept}


def _move_coconut_files(home, backup):
    moved = []
    for name in COCONUT_FILES:
        path = home / name
        if path.is_file() and not path.is_symlink():
            if (backup / name).exists():
                path.unlink()
            else:
                os.replace(path, backup / name)
            moved.append(name)
    return moved


def migrate_bot(default, home, root=None):
    """Run the step for one profile home. Return its report.

    `default` is the active default profile, whose approvals, fallback and model the bots inherited. `root` is Coconut's
    Hermes home, whose MCP servers they inherited. Both are the same folder unless a profile is active there.
    """
    home, default = Path(home), Path(default)
    root = Path(root) if root is not None else default
    path = settings_report(home)
    if path.is_file():
        return json.loads(path.read_text())
    if not home.is_dir() or home.is_symlink():
        return {'status': 'no_profile'}
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from native_mcp import import_servers
    folder = path.parent
    backup = folder / 'backup'
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    _back_up(home, backup)
    _snapshot(home, folder)
    pending = json.loads((folder / PENDING).read_text()) if (folder / PENDING).is_file() else {}
    # A copy that an earlier attempt left unchecked. If its checks fail now, the step undoes that copy too.
    earlier = dict(pending)
    default_config = raw_config(default)
    default_values = _env(default)
    token = set_hermes_home_override(str(home))
    try:
        config = raw_config(home)
        before = deepcopy(config)
        legacy = _legacy_defaults(home, config, backup)
        approvals = _approvals(config, default_config)
        model = _model(config, default_config, default, home, root)
        values = _env(home)
        fallback, writes, fallback_report = _fallback(config, default_config, values, default_values)
        scripts = _shell_init(home, config)
        if fallback:
            # Record the copy before writing it. After a stop, the next attempt finds the routes in config.yaml and
            # copies nothing, but it still runs the checks of this record.
            # The record holds digests, not the values of the copied variables.
            pending = dict(fallback=fallback_report, routes=fallback,
                           secrets=config.get('secrets') if fallback_report.get('copied_secret_sources') else None,
                           written={name: _digest(value) for name, value in writes.items()})
            _write(folder / PENDING, json.dumps(pending, sort_keys=True) + '\n', folder)
        # Write the variables before config.yaml. After a stop between the two writes, the next run finds the
        # settings still missing and repeats both. It does not copy a variable twice.
        for name, value in writes.items():
            _save_env(name, value)
            values[name] = value
        redirects = _redirects(home, values)
        if config != before:
            save_config(home, config)
        if pending:
            _check_fallback(default, home, pending, values)
        # MCP servers last: Hermes's import reads and writes config.yaml and .env itself.
        servers = import_servers(root, home) if (raw_config(root).get('mcp_servers') or {}) else {}
    except BaseException:
        # A bot that is not done keeps no half attempt. The next start runs the step again.
        _restore(home, folder)
        if earlier and _undo_pending(home, earlier):
            (folder / PENDING).unlink(missing_ok=True)
        raise
    finally:
        reset_hermes_home_override(token)
    runs = _move_runs(home)
    files = _move_coconut_files(home, backup)
    report = dict(version=VERSION, status='done', home=str(home), default_home=str(default),
                  legacy_defaults=legacy, approvals=approvals, model=model,
                  fallback=fallback_report or pending.get('fallback', {}),
                  mcp=dict(added=servers.get('added', []), needs_setup=servers.get('needs_setup', []),
                           skipped=servers.get('skipped', [])),
                  login_folders=redirects, removed_shell_init_files=scripts, runs=runs, moved_files=files,
                  backup=str(backup))
    _write(path, json.dumps(report, indent=2, sort_keys=True) + '\n', folder)
    # The report marks the bot as done first. A stop before this cleanup leaves only files that nothing reads.
    (folder / PENDING).unlink(missing_ok=True)
    shutil.rmtree(folder / ATTEMPT, ignore_errors=True)
    return report


def migrate(default, homes, root=None):
    """Run the step for each profile home. Return the reports by bot ID.

    A bot that cannot keep a setting does not stop the others. After all bots, the step raises for those bots, so the
    service does not start with a bot that lost a setting. Their profile files stay as they were before the step.
    """
    reports, failures = {}, {}
    for bot_id, home in homes.items():
        try:
            reports[bot_id] = migrate_bot(default, home, root)
        except MigrationError as error:
            failures[bot_id] = str(error)
    if failures:
        raise MigrationError('Bots not migrated: ' + '; '.join(f'{bot_id}: {reason}' for bot_id, reason in failures.items()))
    return reports
