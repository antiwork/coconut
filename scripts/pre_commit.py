"""Run small local regressions for the staged changes, each test file in its own process."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ('test_idempotency_rejects_payload_and_device_collisions',
         'test_message_request_id_survives_retry_and_restart',
         'test_pairing_expiry_single_use_and_rate_limit')
# Git names the repository of the hook in these, and GIT_DIR outranks the -C of a command. A test that makes a
# repository of its own would commit into this one, and its index, refs and core.bare would change.
HOOK_GIT = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_PREFIX', 'GIT_NAMESPACE',
            'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')


def environment():
    """The tree under test on PYTHONPATH, without the repository of the hook that runs the tests."""
    return {**{key: value for key, value in os.environ.items() if key not in HOOK_GIT},
            'PYTHONPATH': str(ROOT / 'backend')}


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
    env = environment()
    for command in commands:
        print('Local regression:', ' '.join(command), flush=True)
        subprocess.run([sys.executable, *command], cwd=ROOT, env=env, check=True)


if __name__ == '__main__':
    main()
