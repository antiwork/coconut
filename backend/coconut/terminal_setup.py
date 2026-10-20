"""Configure native Hermes shells without modifying Hermes source."""
from pathlib import Path
import os
import shlex
import sys


def command_path(value: str) -> str:
    preferred = [p for p in ('/opt/homebrew/bin', '/opt/homebrew/sbin', '/usr/local/bin', '/usr/local/sbin',
                             str(Path.home() / '.orbstack/bin')) if Path(p).is_dir()]
    return ':'.join(dict.fromkeys([*preferred, *(p for p in value.split(':') if p), '/usr/bin', '/bin', '/usr/sbin', '/sbin']))


# macOS path_helper runs before Hermes sources terminal.shell_init_files.
SHELL_INIT = r'''# Managed by Coconut. Keep package tools ahead of system tools.
_coconut_prepend_path() {
    [ -d "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH"; return 0 ;;
    esac
    local entry remaining="" old_ifs="$IFS"
    IFS=:
    for entry in $PATH; do
        [ "$entry" = "$1" ] || remaining="${remaining:+$remaining:}$entry"
    done
    IFS="$old_ifs"
    PATH="$1${remaining:+:$remaining}"
}
for _coconut_dir in "$HOME/.orbstack/bin" /usr/local/sbin /usr/local/bin /opt/homebrew/sbin /opt/homebrew/bin; do
    _coconut_prepend_path "$_coconut_dir"
done
export PATH
unset _coconut_dir
unset -f _coconut_prepend_path
'''


def configure_terminal(home):
    try:
        from .native_settings import raw_config, save_config
        from .profiles import _write
        from .runtime_environment import CLAUDE_PROFILE_OVERRIDES, prepare_profile
    except ImportError:
        from native_settings import raw_config, save_config
        from profiles import _write
        from runtime_environment import CLAUDE_PROFILE_OVERRIDES, prepare_profile
    home = Path(home)
    config = raw_config(home)
    terminal = config.get('terminal') or {}
    if not isinstance(terminal, dict):
        raise ValueError('Hermes terminal settings must contain a mapping.')
    config['terminal'] = terminal
    files = terminal.get('shell_init_files') or []
    if not isinstance(files, list):
        raise ValueError('Hermes terminal.shell_init_files must contain a list.')
    if not files and terminal.get('auto_source_bashrc', True):
        files = ['~/.profile', '~/.bash_profile', '~/.bashrc']
    # Preserve the files that ~ resolved to before HOME used the real account home.
    files = [str(home / item[2:]) if isinstance(item, str) and item.startswith('~/') else item for item in files]
    script = home / 'coconut-terminal.sh'
    environment = prepare_profile(home)
    exports = 'unset ' + ' '.join(CLAUDE_PROFILE_OVERRIDES) + '\n'
    exports += ''.join(f'export {key}={shlex.quote(value)}\n' for key, value in environment.items())
    _write(script, SHELL_INIT + exports, home)
    google_script = home / 'coconut-google.sh'
    updated = [item for item in files if item not in (str(script), str(google_script))] + [str(script)]
    google_home = os.environ.get('COCONUT_GOOGLE_HOME')
    if google_home:
        command = shlex.join([sys.executable, str(Path(__file__).with_name('google_connection.py')), google_home])
        _write(google_script, '# Managed by Coconut. Google sign-in stays in the phone app.\neval "$(' + command + ')"\n', home)
        updated.append(str(google_script))
    if terminal.get('shell_init_files') != updated:
        terminal['shell_init_files'] = updated
        save_config(home, config)
    for key in CLAUDE_PROFILE_OVERRIDES:
        os.environ.pop(key, None)
    os.environ.update(environment)
    os.environ['PATH'] = command_path(os.environ.get('PATH', ''))
