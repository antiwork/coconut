"""Run the native upgrade checks against a candidate Hermes runtime before it is installed.

docs/hermes-upgrade-checks.md names four checks that need the Mac mini and its model access. CI cannot run them,
and a Coconut bot cannot run them from inside its own Hermes worker. The automatic updater runs this script
outside Hermes, against the runtime that `scripts/hermes.py install` put in its own checkout.

Each check uses disposable profiles in a scratch folder. The script prints one JSON report and exits 0 only
when every check passes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import host  # noqa: E402

# Each check may run model turns. The slowest one takes a few minutes on the Mac mini.
CHECK_SECONDS = 600


def checks():
    """Return (name, program, kind) for each check. `kind` says how the program reports its result."""
    from test_native_profiles import PROGRAMS
    from test_native_relay import PROGRAM as RELAY
    from test_native_routines import PROGRAM as ROUTINES
    return ([('routines', ROUTINES, 'receipt'), ('relay', RELAY, 'receipt')]
            + [(f'profiles-{index + 1}', program, 'pass') for index, program in enumerate(PROGRAMS)])


def passed(kind, result):
    if result.returncode:
        return False
    if kind == 'pass':
        return 'PASS:' in result.stdout
    receipts = [line.split('=', 1)[1] for line in result.stdout.splitlines() if line.startswith('COCONUT_CREATION_RESULT=')]
    return len(receipts) == 1 and json.loads(receipts[0]).get('passed') is True


def run_checks(checkout: Path, live: Path) -> dict:
    source = checkout / '.runtime/hermes/source'
    python = checkout / '.runtime/hermes/venv/bin/python'
    if not python.is_file() or not (source / '.git').exists():
        raise SystemExit('The candidate runtime is missing. Run scripts/hermes.py install in this checkout first.')
    # Model checks use the credentials of the installed service. Read them in memory only.
    host.load_credentials(live / '.runtime/host')
    report = dict(revision=json.loads((checkout / '.runtime/hermes/install.json').read_text())['commit'], checks=[])
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
                result = subprocess.run([str(python), '-c', program, str(root), str(checkout)], env=env,
                                        text=True, capture_output=True, timeout=CHECK_SECONDS)
                ok = passed(kind, result)
                tail = dict(out=result.stdout[-1500:], err=result.stderr[-1500:])
            except subprocess.TimeoutExpired:
                ok, tail = False, dict(out='', err=f'The check ran longer than {CHECK_SECONDS} seconds.')
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
