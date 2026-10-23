"""Find the Hermes that the person installed, check it, and describe it.

Coconut does not install its own Hermes. The service runs under the Python of the person's Hermes. Without a usable
Hermes, the service starts in refuse mode under the standard Python and imports no Hermes code.

This module uses the standard library only, because host.py imports it before it knows which Python to run.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND = PROJECT_ROOT / 'backend'
# The shared model home of the bots stays in the folder of the old private runtime until bots become Hermes profiles.
DEFAULT_HOME = PROJECT_ROOT / '.runtime/hermes/terminal'
# Coconut wrote this line into its own `hermes` command. That command is not the person's Hermes.
LAUNCHER_MARKER = b'Coconut owns this Hermes launcher.'
MODULES = 'run_agent, hermes_cli, hermes_state, cron, tools, agent, gateway, dotenv, ruamel.yaml, mcp, PIL'
INSTALL_HINT = 'Install Hermes from https://github.com/NousResearch/hermes-agent.'
STATUS_FILE = 'hermes.json'
PROBE_SECONDS = 60
CONTRACT_SECONDS = 300
# The probe and the check get these variables from the service environment. Model keys stay out.
ENVIRONMENT_KEYS = ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SSL_CERT_FILE', 'SSL_CERT_DIR')

PROBE = (
    'import importlib.metadata, json, os, sysconfig\n'
    'import ' + MODULES + '\n'
    'try:\n'
    '    version = importlib.metadata.version("hermes-agent")\n'
    'except importlib.metadata.PackageNotFoundError:\n'
    '    version = None\n'
    'print(json.dumps({"root": os.path.dirname(os.path.realpath(run_agent.__file__)), "version": version,\n'
    '                  "packages": sysconfig.get_path("purelib")}))\n'
)

CONTRACT = '''import json
import sys
def no_network(event, arguments):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('The Hermes contract check cannot use the network.')
sys.addaudithook(no_network)
sys.path.insert(0, sys.argv[1])
from coconut.hermes_contract import verify_contract
print(json.dumps(verify_contract()))
'''


class HermesUnavailable(Exception):
    """Coconut cannot run on this Mac's Hermes. The status is "missing" or "incompatible"."""

    def __init__(self, status: str, reason: str, *, version: str | None = None, lasting: bool = True):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.version = version
        # A timeout can pass on the next start. Only a lasting result is kept for the same Hermes and Coconut code.
        self.lasting = lasting

    def record(self) -> dict:
        return {'status': self.status, 'version': self.version, 'reason': self.reason}


@dataclass(frozen=True)
class HermesRuntime:
    python: Path
    root: Path
    version: str | None
    home: Path
    packages: str | None = None

    def describe(self) -> dict:
        return {'python': str(self.python), 'root': str(self.root), 'version': self.version, 'home': str(self.home)}


def hermes_home(environ=None, home=None) -> Path:
    environ = os.environ if environ is None else environ
    return Path(environ.get('HERMES_HOME') or Path(home or Path.home()) / '.hermes').expanduser()


def launcher_python(command: Path) -> Path | None:
    """Return the Python that a `hermes` console script runs, or None for Coconut's own launcher."""
    try:
        with command.resolve().open('rb') as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if LAUNCHER_MARKER in head:
        return None
    text = head.decode('utf-8', errors='replace')
    words = text[2:].splitlines()[0].split() if head.startswith(b'#!') else []
    if words and Path(words[0]).name == 'env' and len(words) > 1:
        found = shutil.which(words[1])
        return Path(found) if found else None
    if words and Path(words[0]).name == 'sh':
        # pip writes this form when the interpreter path is too long for a shebang line.
        match = re.search(r"""^'''exec' ['"]?([^'"\n]+?)['"]? "\$0" "\$@"$""", text, re.MULTILINE)
        if match:
            return Path(match[1])
    elif words:
        return Path(words[0])
    # A console script of a virtual environment sits beside the Python of that environment.
    sibling = command.resolve().parent / 'python'
    return sibling if sibling.is_file() else None


def candidates(environ=None, home=None) -> list[tuple[Path, Path | None, bool]]:
    """List (python, expected root, explicit) in search order. Nothing here runs Hermes."""
    environ = os.environ if environ is None else environ
    home = Path(home or Path.home())
    found = []
    python, root = environ.get('COCONUT_HERMES_PYTHON'), environ.get('COCONUT_HERMES_ROOT')
    if python or root:
        root = Path(root).expanduser() if root else None
        if python:
            found.append((Path(python).expanduser(), root, True))
        else:
            found.extend((root / name / 'bin/python', root, True) for name in ('venv', '.venv'))
    command = shutil.which('hermes', path=environ.get('PATH', os.defpath))
    if command:
        python = launcher_python(Path(command))
        if python is not None:
            found.append((python, None, False))
    source = home / '.hermes/hermes-agent'
    found.extend((source / name / 'bin/python', source, False) for name in ('venv', '.venv'))
    result, seen = [], set()
    for python, root, explicit in found:
        # The private runtime that older Coconut versions installed is not the person's Hermes.
        if not explicit and PROJECT_ROOT / '.runtime' in python.absolute().parents:
            continue
        if str(python) not in seen:
            seen.add(str(python))
            result.append((python, root, explicit))
    return result


def locate(environ=None, home=None) -> tuple[Path, Path | None] | None:
    """Return the first Hermes Python that exists, without running it."""
    for python, root, _ in candidates(environ, home):
        if python.is_file() and os.access(python, os.X_OK):
            return python, root
    return None


def configured(python=None, root=None, environ=None) -> tuple[Path | None, Path | None]:
    """Return the Hermes Python and source folder without running Hermes. Explicit values win.

    The service passes the values that find() checked. This default serves tools and tests that start a provider directly.
    """
    if python is None or root is None:
        found = locate(environ)
        if found is not None:
            # The Hermes installer keeps the virtual environment inside the source folder: <root>/venv/bin/python.
            python, root = python or found[0], root or found[1] or found[0].parent.parent.parent
    return (Path(python) if python else None), (Path(root) if root else None)


@contextmanager
def isolated(environ=None):
    """Yield an environment and a folder for a Hermes check. Hermes writes only into the temporary home."""
    environ = os.environ if environ is None else environ
    with tempfile.TemporaryDirectory(prefix='coconut-hermes-check-') as temporary:
        folder = Path(temporary)
        (folder / 'home').mkdir()
        environment = {key: environ[key] for key in ENVIRONMENT_KEYS if environ.get(key)}
        environment.update(HERMES_HOME=str(folder / 'home'), TMPDIR=temporary, HERMES_DISABLE_TELEMETRY='1',
                           PYTHONUNBUFFERED='1')
        yield environment, folder


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    return (lines[-1] if lines else 'No error output.')[-500:]


def probe(python: Path, environ=None, timeout: float = PROBE_SECONDS) -> dict:
    """Import the Hermes modules that Coconut uses in an isolated interpreter. Return the root and the version."""
    with isolated(environ) as (environment, folder):
        try:
            result = subprocess.run([str(python), '-I', '-B', '-c', PROBE], cwd=folder, env=environment,
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HermesUnavailable('incompatible', f'Hermes did not load within {timeout:.0f} seconds.', lasting=False) from None
        except OSError as error:
            raise HermesUnavailable('incompatible', f'The Hermes Python cannot start: {error.strerror}.') from None
    if result.returncode:
        raise HermesUnavailable('incompatible', 'The installed Hermes cannot load a module that Coconut uses: '
                                + last_line(result.stderr))
    try:
        info = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(info.get('root'), str):
            raise ValueError()
    except (ValueError, IndexError, AttributeError):
        raise HermesUnavailable('incompatible', 'The installed Hermes returned an invalid description.') from None
    return info


def git_commit(root: Path) -> str | None:
    """Read the checked-out commit of a Hermes source folder without running Git."""
    git = root / '.git'
    try:
        head = (git / 'HEAD').read_text().strip()
        if not head.startswith('ref: '):
            return head if re.fullmatch(r'[0-9a-f]{40}', head) else None
        reference = head[5:]
        path = git / reference
        if path.is_file():
            return path.read_text().strip()
        for line in (git / 'packed-refs').read_text().splitlines():
            if line.endswith(' ' + reference):
                return line.split()[0]
    except OSError:
        pass
    return None


def version_name(version: str | None, root: Path) -> str | None:
    if version and version != '0.0.0':
        return version
    commit = git_commit(root)
    return f'commit {commit[:12]}' if commit else version


def find(environ=None, home=None, timeout: float = PROBE_SECONDS) -> HermesRuntime:
    """Return the first usable Hermes in this order: the COCONUT_HERMES_* settings, `hermes` on PATH,
    then ~/.hermes/hermes-agent. Raise HermesUnavailable when none can run Coconut."""
    problem = None
    found_candidates = candidates(environ, home)
    explicit_count = sum(explicit for _, _, explicit in found_candidates)
    for index, (python, root, explicit) in enumerate(found_candidates):
        # An explicit setting is final: Coconut never runs on another Hermes than the one that the settings name.
        last_explicit = explicit and index == explicit_count - 1
        if not python.is_file() or not os.access(python, os.X_OK):
            if last_explicit:
                raise problem or HermesUnavailable('incompatible', 'COCONUT_HERMES_PYTHON or COCONUT_HERMES_ROOT names no Hermes Python.')
            continue
        try:
            info = probe(python, environ, timeout)
            found = Path(info['root'])
            if root is not None and found.resolve() != root.resolve():
                raise HermesUnavailable('incompatible', 'The Hermes Python loads Hermes from another folder than its source folder.')
        except HermesUnavailable as error:
            problem = problem or error
            if last_explicit:
                raise problem
            continue
        return HermesRuntime(python=python, root=found, version=version_name(info.get('version'), found),
                             home=hermes_home(environ, home), packages=info.get('packages'))
    if problem is not None:
        raise problem
    raise HermesUnavailable('missing', 'Coconut found no Hermes on this Mac. ' + INSTALL_HINT)


def check(runtime: HermesRuntime, environ=None, timeout: float = CONTRACT_SECONDS) -> dict:
    """Run Coconut's contract check against the Hermes. It sends no model request and uses a temporary home."""
    with isolated(environ) as (environment, folder):
        try:
            result = subprocess.run([str(runtime.python), '-I', '-B', '-c', CONTRACT, str(BACKEND)], cwd=folder,
                                    env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HermesUnavailable('incompatible', f'The Hermes contract check did not finish within {timeout:.0f} seconds.',
                                    version=runtime.version, lasting=False) from None
        except OSError as error:
            raise HermesUnavailable('incompatible', f'The Hermes Python cannot start: {error.strerror}.',
                                    version=runtime.version) from None
    if result.returncode:
        raise HermesUnavailable('incompatible', 'This Coconut version does not work with the installed Hermes: '
                                + last_line(result.stderr), version=runtime.version)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise HermesUnavailable('incompatible', 'The Hermes contract check returned no result.', version=runtime.version) from None


def code_digest() -> str:
    """The contract check runs Coconut code too, so a result counts only for this code."""
    digest = hashlib.sha256()
    for path in sorted((BACKEND / 'coconut').rglob('*.py')):
        digest.update(str(path.relative_to(BACKEND)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def fingerprint(runtime: HermesRuntime) -> str:
    try:
        packages = Path(runtime.packages).stat().st_mtime_ns if runtime.packages else None
    except OSError:
        packages = None
    parts = [str(runtime.python), str(runtime.root), runtime.version, git_commit(runtime.root), packages, code_digest()]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def read_status(data_dir) -> dict | None:
    path = Path(data_dir) / STATUS_FILE
    try:
        if path.is_symlink():
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get('status') in ('ok', 'missing', 'incompatible') else None


def write_status(data_dir, value: dict) -> None:
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile('w', dir=folder, prefix='.hermes-', delete=False) as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, folder / STATUS_FILE)


def saved_still_valid(saved: dict | None, environ=None, home=None) -> bool:
    """Return True when a saved lasting result describes the Hermes that the search finds first, unchanged.

    This check starts no program, so a restart skips the import probe as well as the contract check.
    """
    if not saved or not saved.get('fingerprint') or not saved.get('python') or not saved.get('root'):
        return False
    found = locate(environ, home)
    if found is None or str(found[0]) != saved['python'] or (found[1] is not None and found[1].resolve() != Path(saved['root'])):
        return False
    runtime = HermesRuntime(python=found[0], root=Path(saved['root']), version=saved.get('version'),
                            home=hermes_home(environ, home), packages=saved.get('packages'))
    return fingerprint(runtime) == saved['fingerprint']


def status(data_dir=None, environ=None, home=None, *, save=True) -> dict:
    """Find and check the Hermes. Return {status, version, reason} and, when status is "ok", {python, root, home}.

    With data_dir, a lasting result for the same Hermes and Coconut code is reused, so a restart does not run the
    contract check again. The service saves its result there. An update checks new code with save=False: its result
    must not replace the status of the running version, which decides a rollback (see deploy.check_hermes).
    """
    saved = read_status(data_dir) if data_dir is not None else None
    if saved_still_valid(saved, environ, home):
        return saved
    try:
        runtime = find(environ, home)
    except HermesUnavailable as error:
        result = error.record()
    else:
        key = fingerprint(runtime)
        if saved and saved.get('fingerprint') == key:
            return saved
        result = dict(runtime.describe(), status='ok', reason=None, fingerprint=key, packages=runtime.packages)
        try:
            check(runtime, environ)
        except HermesUnavailable as error:
            result.update(error.record())
            if not error.lasting:
                result.pop('fingerprint')
    if data_dir is not None and save:
        write_status(data_dir, result)
    return result


def public(value: dict | None) -> dict:
    """The part of a status that the phone receives. It contains no file paths."""
    value = value or {}
    return {'status': value.get('status', 'missing'), 'version': value.get('version'), 'reason': value.get('reason')}


def remove_coconut_launcher(home=None) -> bool:
    """Delete ~/.local/bin/hermes when Coconut wrote it. Another program at that path stays."""
    path = Path(home or Path.home()) / '.local/bin/hermes'
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open('rb') as handle:
            if LAUNCHER_MARKER not in handle.read(4096):
                return False
        path.unlink()
    except OSError:
        return False
    return True
