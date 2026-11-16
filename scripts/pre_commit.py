"""Run small local regressions for the staged changes, each test file in its own process."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ('test_idempotency_rejects_payload_and_device_collisions',
         'test_message_request_id_survives_retry_and_restart',
         'test_pairing_expiry_single_use_and_rate_limit')


def checks(paths):
    tests = set()
    service = any(p.startswith(('backend/', 'hermes_plugin/')) for p in paths)
    for path in paths:
        file = Path(path)
        if file.parent.as_posix() in ('backend/tests', 'scripts') and file.name.startswith('test_') and file.suffix == '.py' and (ROOT / file).is_file():
            tests.add(path)
        elif file.suffix == '.py' and file.parent.as_posix() in ('backend/coconut', 'scripts'):
            candidate = ('backend/tests/' if path.startswith('backend/') else 'scripts/') + f'test_{file.stem}.py'
            # The service has a large suite. Its three smoke cases run first instead.
            if file.stem != 'service' and (ROOT / candidate).is_file():
                tests.add(candidate)
    commands = []
    if service:
        commands.append(['-m', 'unittest', 'discover', '-s', 'backend/tests', '-p', 'test_service.py',
                         *[part for name in SMOKE for part in ('-k', name)]])
    commands += [['-m', 'unittest', 'discover', '-s', str(Path(path).parent), '-p', Path(path).name]
                 for path in sorted(tests)]
    return commands


def main():
    paths = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMRD', '-z'], cwd=ROOT)
    commands = checks(paths.decode().rstrip('\0').split('\0'))
    if not commands:
        print('No service regressions needed for these staged files. Run app checks locally when needed.')
        return
    environment = dict(os.environ, PYTHONPATH=str(ROOT / 'backend'))
    for command in commands:
        print('Local regression:', ' '.join(command), flush=True)
        subprocess.run([sys.executable, *command], cwd=ROOT, env=environment, check=True)


if __name__ == '__main__':
    main()
