"""Run the native upgrade checks against a candidate Hermes runtime before it is installed.

docs/hermes-upgrade-checks.md names four checks that need the Mac mini, and some need its model access. CI cannot run them,
and a Coconut bot cannot run them from inside its own Hermes worker. The automatic updater runs this script
outside Hermes, against the runtime that `scripts/hermes.py install` put in its own checkout.

Each check uses disposable profiles in a scratch folder. The script prints one JSON report and exits 0 only
when every check passes.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import host  # noqa: E402

# Each check may run model turns. The slowest one takes a few minutes on the Mac mini.
CHECK_SECONDS = 600
# After a timeout, read the remaining output for this long. A process that is still alive can keep the pipes open.
DRAIN_SECONDS = 30


def checks():
    """Return (name, program, kind) for each check. `kind` says how the program reports its result."""
    from test_native_profiles import PROGRAMS
    from test_native_relay import PROGRAM as RELAY
    from test_native_routines import PROGRAM as ROUTINES
    from test_script_routines import PROGRAM as SCRIPTS
    return ([('routines', ROUTINES, 'receipt'), ('script-routines', SCRIPTS, 'receipt'), ('relay', RELAY, 'receipt')]
            + [(f'profiles-{index + 1}', program, 'pass') for index, program in enumerate(PROGRAMS)])


def descendants(pid):
    """Return the IDs of every process below `pid`. Hermes workers start their own sessions, so a group kill misses them."""
    listing = subprocess.run(['ps', '-A', '-o', 'pid=,ppid='], capture_output=True, text=True, timeout=30).stdout
    children = {}
    for line in listing.splitlines():
        with contextlib.suppress(ValueError):
            child, parent = map(int, line.split())
            children.setdefault(parent, []).append(child)
    found, pending = [], [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            found.append(child)
            pending.append(child)
    return found


def stop_tree(pid):
    """Stop a process and every process below it. A failed process listing still stops the process itself."""
    try:
        found = descendants(pid)
    except (OSError, subprocess.SubprocessError):
        found = []
    for identity in [*found, pid]:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(identity, signal.SIGKILL)


def run_tree(command, *, env=None, cwd=None, timeout):
    """Run a command. On a timeout, stop it and every process it started, so no check keeps running."""
    process = subprocess.Popen(command, env=env, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_tree(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=DRAIN_SECONDS)
        except subprocess.TimeoutExpired as late:
            # A process that started after the listing can keep the pipes open. Stop reading them.
            stdout, stderr = text(late.stdout), text(late.stderr)
            process.stdout.close()
            process.stderr.close()
            process.wait()
        raise subprocess.TimeoutExpired(command, timeout, stdout, stderr) from None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def text(output):
    """Return partial output as text. A communicate timeout gives bytes even in text mode."""
    return output.decode(errors='replace') if isinstance(output, bytes) else output or ''


def passed(kind, result):
    if result.returncode:
        return False
    if kind == 'pass':
        return 'PASS:' in result.stdout
    receipts = [line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('COCONUT_CREATION_RESULT=')]
    try:
        return len(receipts) == 1 and json.loads(receipts[0]).get('passed') is True
    except (ValueError, AttributeError):
        # A broken marker line fails this check only. The other checks still run.
        return False


def run_checks(checkout: Path, live: Path) -> dict:
    source = checkout / '.runtime/hermes/source'
    python = checkout / '.runtime/hermes/venv/bin/python'
    missing = 'The candidate runtime is missing. Run scripts/hermes.py install in this checkout first.'
    if not python.is_file() or not (source / '.git').exists():
        raise SystemExit(missing)
    try:
        # hermes.py writes the receipt last. A stopped install leaves the other folders without it.
        revision = json.loads((checkout / '.runtime/hermes/install.json').read_text())['commit']
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(missing) from None
    # Model checks use the credentials of the installed service. Read them in memory only.
    host.load_credentials(live / '.runtime/host')
    report = dict(revision=revision, checks=[])
    with tempfile.TemporaryDirectory(prefix='candidate-checks-', dir=checkout / '.runtime') as directory:
        root = Path(directory)
        for folder in ('.runtime/hermes', '.runtime/checks', '.runtime/host'):
            (root / folder).mkdir(parents=True)
        (root / '.runtime/hermes/source').symlink_to(source)
        (root / '.runtime/hermes/venv').symlink_to(checkout / '.runtime/hermes/venv')
        (root / 'backend').symlink_to(checkout / 'backend')
        (root / 'scripts').symlink_to(checkout / 'scripts')
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(checkout / 'backend'), str(source))))
        for name, program, kind in checks():
            try:
                result = run_tree([str(python), '-c', program, str(root), str(checkout)], env=env, timeout=CHECK_SECONDS)
                ok = passed(kind, result)
                tail = dict(out=result.stdout[-1500:], err=result.stderr[-1500:])
            except subprocess.TimeoutExpired as error:
                # The partial output shows where the check stopped.
                ok, tail = False, dict(out=text(error.stdout)[-1500:],
                                       err=text(error.stderr)[-1500:] + f'\nThe check ran longer than {CHECK_SECONDS} seconds.')
            report['checks'].append(dict(name=name, passed=ok, **tail))
            print(json.dumps(dict(name=name, passed=ok)), file=sys.stderr, flush=True)
    report['passed'] = bool(report['checks']) and all(check['passed'] for check in report['checks'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkout', type=Path, required=True, help='Coconut checkout with the candidate .runtime/hermes.')
    parser.add_argument('--live', type=Path, required=True, help='Installed Coconut folder, for its model credentials.')
    args = parser.parse_args()
    report = run_checks(args.checkout.resolve(), args.live.resolve())
    print('COCONUT_HERMES_CHECKS=' + json.dumps(report))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
