#!/usr/bin/env python3
"""Expose Coconut's removable Hermes runtime in Terminal."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import sys
import tempfile

import deploy
import host

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT_KEYS = {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TERM', 'COLORTERM', 'TERM_PROGRAM',
                    'TERM_PROGRAM_VERSION', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'EDITOR', 'VISUAL'}


def safe(path, boundary):
    return deploy.safe_path(path, boundary)


def private(path):
    safe(path, ROOT)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def wrapper_path(home=None):
    home = home or Path.home()
    return safe(home / '.local/bin/hermes', home)


def wrapper_bytes():
    command = ['/usr/bin/python3', str(ROOT / 'scripts/terminal.py'), 'launch', '--']
    return ('#!/bin/sh\n# Coconut owns this Hermes launcher.\nexec ' + shlex.join(command) + ' "$@"\n').encode()


def install_command(home=None):
    home = home or Path.home()
    path = wrapper_path(home)
    content = wrapper_bytes()
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise SystemExit('A different hermes command already exists. Coconut left it unchanged.')
        return path
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.coconut-hermes-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.chmod(0o700)
            # Linking prevents an existing command from being overwritten during installation.
            os.link(temporary, path)
            deploy.sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def uninstall_command(home=None):
    home = home or Path.home()
    path = home / '.local/bin/hermes'
    try:
        safe(path, home)
    except deploy.DeployError:
        return False
    if not path.is_file() or path.read_bytes() != wrapper_bytes():
        return False
    path.unlink()
    deploy.sync_directory(path.parent)
    return True


def model_access(data):
    path = safe(data / 'credentials.json', ROOT)
    if not path.exists():
        return {}
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise SystemExit('The saved model credentials must belong to this account and have private permissions.')
        value = json.loads(handle.read(65537))
    if not isinstance(value, dict):
        raise SystemExit('The saved model credentials have an invalid format.')
    return {key: item for key, item in value.items() if key in host.MODEL_KEYS and isinstance(item, str) and item}


@contextmanager
def runtime_lease(exclusive=False):
    directory = private(ROOT / '.runtime/hermes')
    descriptor = os.open(safe(directory / 'terminal.lock', ROOT), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Close Hermes Terminal sessions before removing its runtime.') from None
        os.set_inheritable(descriptor, True)
        yield
    finally:
        os.close(descriptor)


def read_database(data):
    path = safe(data / 'coconut.sqlite3', ROOT)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        bots = [json.loads(row[0]) for row in connection.execute('SELECT data FROM bots')]
    return bots


def select_bot(bots, value):
    candidates = [bot for bot in bots if not bot.get('_deleted')
                  and (bot['id'] == value or bot['name'].casefold() == value.casefold())]
    if len(candidates) != 1:
        raise SystemExit('Use one exact bot name or profile ID from hermes --coconut-bots.')
    return candidates[0]


def private_auth_path(path):
    path = safe(path, ROOT)
    if path.exists():
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise SystemExit('Native Hermes credentials must belong to this account and have private permissions.')
    return path


def run_bot_cli():
    """Use canonical model credentials while retaining the isolated bot home."""
    from coconut.model_catalog import HermesModelCatalog, native_home, _read_profile
    root = safe(Path(os.environ.pop('COCONUT_TERMINAL_AUTH_ROOT')), ROOT)
    selected_home = safe(Path(os.environ.pop('COCONUT_TERMINAL_AUTH_HOME')), root)
    if native_home(root) != selected_home:
        raise SystemExit('The active Hermes profile changed. Run this command again.')
    selected = json.loads(os.environ.pop('COCONUT_TERMINAL_MODEL'))
    effort = json.loads(os.environ.pop('COCONUT_TERMINAL_EFFORT'))
    private_auth_path(selected_home / 'auth.json')
    private_auth_path(root / 'auth.json')
    from hermes_cli import auth
    # Native refresh keeps its file lock and writes to the same canonical store.
    auth._auth_file_path = lambda: private_auth_path(selected_home / 'auth.json')
    auth._global_auth_file_path = lambda: private_auth_path(root / 'auth.json') if root != selected_home else None
    from agent import anthropic_credentials
    anthropic_credentials._get_hermes_oauth_file = lambda: private_auth_path(selected_home / '.anthropic_oauth.json')
    anthropic_credentials._root_hermes_oauth_file = lambda: private_auth_path(root / '.anthropic_oauth.json') if root != selected_home else None
    registration = auth.PROVIDER_REGISTRY.get(selected['provider'])
    if registration:
        from dotenv import dotenv_values
        path = private_auth_path(selected_home / '.env')
        values = dotenv_values(path) if path.exists() else {}
        for key in registration.api_key_env_vars:
            if values.get(key) is not None:
                os.environ[key] = values[key]
    else:
        from hermes_cli import runtime_provider
        original = runtime_provider.resolve_runtime_provider
        catalog = HermesModelCatalog(ROOT / '.runtime/host',
                                     python=ROOT / '.runtime/hermes/venv/bin/python',
                                     source=ROOT / '.runtime/hermes/source', env=os.environ)

        def resolve_runtime(*, requested=None, explicit_api_key=None, explicit_base_url=None, target_model=None):
            if (requested not in (None, 'auto', selected['provider'])
                    or target_model not in (None, selected['default']) or explicit_api_key is not None
                    or explicit_base_url not in (None, selected.get('base_url'))):
                return original(requested=requested, explicit_api_key=explicit_api_key,
                                explicit_base_url=explicit_base_url, target_model=target_model)
            # Native provider definitions can contain inline keys. Keep them out of bot files.
            return catalog.resolve_runtime({'model': selected['default'], 'provider': selected['provider'],
                                            'reasoning_effort': effort})

        runtime_provider.resolve_runtime_provider = resolve_runtime
    from hermes_cli import config as native_config
    original_load = native_config.load_config
    inherited = {'model': selected['default'], 'provider': selected['provider'],
                 'base_url': selected.get('base_url'), 'reasoning_effort': effort}

    def load_config(*args, **kwargs):
        config = original_load(*args, **kwargs)
        current = _read_profile(os.environ['HERMES_HOME'], inherited)['effective_model'] or inherited
        model = dict(config['model']) if isinstance(config.get('model'), dict) else {}
        model.update(default=current['model'], provider=current['provider'])
        if current.get('base_url'):
            model['base_url'] = current['base_url']
        config['model'] = model
        agent = dict(config.get('agent') or {})
        if current.get('reasoning_effort') is None:
            agent.pop('reasoning_effort', None)
        else:
            agent['reasoning_effort'] = current['reasoning_effort']
        config['agent'] = agent
        return config

    # Opening a bot must not turn inheritance into a saved override or replace native fields.
    native_config.load_config = load_config
    from hermes_cli.main import main
    main()


def _launch(arguments):
    data = safe(ROOT / '.runtime/host', ROOT)
    if arguments == ['--coconut-bots']:
        for bot in read_database(data):
            if not bot.get('_deleted'):
                print(bot['name'] + '\t' + bot['id'])
        return
    bot = None
    if arguments[:1] == ['--coconut-bot']:
        if len(arguments) < 2:
            raise SystemExit('Use hermes --coconut-bot NAME_OR_ID followed by optional Hermes arguments.')
        bot = select_bot(read_database(data), arguments[1])
        arguments = arguments[2:]
        if any(item in {'--profile', '-p'} or item.startswith('--profile=') for item in arguments):
            raise SystemExit('Choose either a Coconut bot or a native Hermes profile for this command.')
    source = safe(ROOT / '.runtime/hermes/source', ROOT)
    python = ROOT / '.runtime/hermes/venv/bin/python'
    if not python.is_file() or not (source / 'hermes_cli/main.py').is_file():
        raise SystemExit('Install the project runtime with python3 scripts/hermes.py install first.')
    if (source / '.env').exists() or (source / '.env').is_symlink():
        raise SystemExit('The Hermes checkout contains shared credentials. Use a clean Coconut runtime checkout.')
    credentials = model_access(data)
    environment = {key: value for key, value in os.environ.items() if key in ENVIRONMENT_KEYS}
    environment.update(credentials)
    catalog = None
    if bot:
        if (data / 'moved-to.json').exists() or (data / 'moved-to.json').is_symlink():
            raise SystemExit('This bot moved to another Mac. Open its Terminal profile on that Mac.')
        sys.path.insert(0, str(ROOT / 'backend'))
        from coconut.model_catalog import HermesModelCatalog, ModelCatalogError, native_home
        catalog = HermesModelCatalog(data, python=python, source=source, env=environment)
    lease = None
    try:
        if bot:
            lease = os.open(safe(data / 'service.lock', ROOT), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit('Stop Coconut before opening its bot profile in Terminal. Bare hermes uses a separate profile.') from None
            if (data / 'moved-to.json').exists() or (data / 'moved-to.json').is_symlink():
                raise SystemExit('This bot moved to another Mac. Open its Terminal profile on that Mac.')
            bots = read_database(data)
            bot = select_bot(bots, bot['id'])
            from coconut.profiles import HermesProfiles
            profile = catalog.read_profile(bot['id'])
            if profile.get('error'):
                raise ModelCatalogError(profile['error'])
            selected = profile.get('effective_model') or catalog.get_default()
            auth_home = native_home(catalog.root)
            HermesProfiles(data).sync(bot)
            base = private(data / 'bots' / bot['id'])
            hermes_home = private(base / 'home')
            user_home = hermes_home
            # The explicit default ignores Hermes' sticky active_profile file for this bot.
            model_arguments = ['--model', selected['model'], '--provider', selected['provider']]
            if selected.get('reasoning_effort') is not None:
                model_arguments += ['--reasoning', selected['reasoning_effort']]
            arguments = ['--profile', 'default', *model_arguments, *arguments]
            print('Terminal chats do not appear in Coconut.', file=sys.stderr, flush=True)
        else:
            base = private(ROOT / '.runtime/hermes/terminal')
            hermes_home = private(base / 'hermes')
            user_home = private(base / 'home')
        environment.update(HOME=str(user_home), HERMES_REAL_HOME=str(user_home), HERMES_HOME=str(hermes_home),
                           TMPDIR=str(private(base / 'tmp')), PYTHONPATH=str(source), PYTHONUNBUFFERED='1',
                           HERMES_DISABLE_TELEMETRY='1')
        environment['PATH'] = str(python.parent) + os.pathsep + environment.get('PATH', '/usr/bin:/bin')
        os.chdir(private(base / 'workspace'))
        if lease is not None:
            os.set_inheritable(lease, True)
        bootstrap = 'from hermes_cli.main import main; main()'
        if bot:
            model_config = {'default': selected['model'], 'provider': selected['provider']}
            if selected.get('base_url'):
                model_config['base_url'] = selected['base_url']
            environment['COCONUT_TERMINAL_MODEL'] = json.dumps(model_config)
            environment['COCONUT_TERMINAL_EFFORT'] = json.dumps(selected.get('reasoning_effort'))
            environment['COCONUT_TERMINAL_AUTH_ROOT'] = str(catalog.root)
            environment['COCONUT_TERMINAL_AUTH_HOME'] = str(auth_home)
            environment['PYTHONPATH'] = os.pathsep.join(map(str, (source, ROOT / 'backend', ROOT / 'scripts')))
            bootstrap = 'from terminal import run_bot_cli; run_bot_cli()'
        command = [str(python), '-c', bootstrap, *arguments]
        os.execve(python, command, environment)
    finally:
        if lease is not None:
            os.close(lease)


def launch(arguments):
    if arguments == ['--coconut-bots']:
        return _launch(arguments)
    with runtime_lease():
        return _launch(arguments)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('install-command')
    commands.add_parser('uninstall-command')
    start = commands.add_parser('launch')
    start.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.command == 'install-command':
            path = install_command()
            print('Installed ' + str(path) + '. Run hermes in Terminal.')
        elif args.command == 'uninstall-command':
            print('Removed Coconut’s hermes command.' if uninstall_command() else 'No matching Coconut command was removed.')
        else:
            launch(args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments)
    except (deploy.DeployError, OSError, ValueError, sqlite3.Error) as error:
        if isinstance(error, deploy.DeployError):
            raise SystemExit(str(error)) from None
        raise SystemExit('The Hermes launcher could not read its local configuration. No model credential was displayed.') from None


if __name__ == '__main__':
    main()
