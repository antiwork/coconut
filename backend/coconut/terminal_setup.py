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


# The scripts that the native shell sources after the profile's own terminal.shell_init_files.
_scripts: list[str] = []


def configure_terminal(home, directory=None):
    """Prepare the native shell of one bot. Coconut writes nothing into the Hermes profile.

    Hermes's register_task_env_overrides carries only the sandbox image, the backend and cwd, so it cannot carry shell
    setup. Coconut writes its scripts into its own folder of the bot (`directory`, by default profiles.bot_folder of
    the profile) and adds them, in this process only, to the files that the native shell sources.
    """
    try:
        from .profiles import _safe, _write, bot_folder
        from .runtime_environment import CLAUDE_PROFILE_OVERRIDES, prepare_profile
    except ImportError:
        from profiles import _safe, _write, bot_folder
        from runtime_environment import CLAUDE_PROFILE_OVERRIDES, prepare_profile
    home = Path(home)
    directory = Path(directory) if directory is not None else bot_folder(home)
    folder = directory / 'terminal'
    _safe(folder, directory)
    folder.mkdir(exist_ok=True, mode=0o700)
    environment = prepare_profile(home)
    exports = 'unset ' + ' '.join(CLAUDE_PROFILE_OVERRIDES) + '\n'
    exports += ''.join(f'export {key}={shlex.quote(value)}\n' for key, value in environment.items())
    script = folder / 'coconut-terminal.sh'
    _write(script, SHELL_INIT + exports, directory)
    scripts = [str(script)]
    google_script = folder / 'coconut-google.sh'
    google_home = os.environ.get('COCONUT_GOOGLE_HOME')
    if google_home:
        command = shlex.join([sys.executable, str(Path(__file__).with_name('google_connection.py')), google_home])
        _write(google_script, '# Managed by Coconut. Google sign-in stays in the phone app.\neval "$(' + command + ')"\n', directory)
        scripts.append(str(google_script))
    github_token = os.environ.get('COCONUT_GITHUB_TOKEN_FILE')
    if github_token:
        try:
            from .github_connection import shell_hook
        except ImportError:
            from github_connection import shell_hook
        github_script = folder / 'coconut-github.sh'
        _write(github_script, shell_hook(github_token), directory)
        scripts.append(str(github_script))
    install_shell_init(scripts)
    for key in CLAUDE_PROFILE_OVERRIDES:
        os.environ.pop(key, None)
    os.environ.update(environment)
    os.environ['PATH'] = command_path(os.environ.get('PATH', ''))


def install_shell_init(scripts):
    """Source these scripts last in every native shell of this process. The profile's own list stays first."""
    from tools.environments import local
    _scripts[:] = scripts
    if getattr(local._resolve_shell_init_files, 'coconut_scripts', None) is _scripts:
        return
    original = local._resolve_shell_init_files

    def resolve():
        files = original()
        return files + [path for path in _scripts if path not in files and os.path.isfile(path)]

    resolve.coconut_scripts = _scripts
    local._resolve_shell_init_files = resolve
