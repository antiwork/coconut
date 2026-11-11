"""Decide whether the changed files of a pull request can change the result of the service checks.

The jobs `contracts` and `native-kanban` of .github/workflows/hermes-compatibility.yml run backend/tests and
scripts/test_testflight.py. These tests read backend/, hermes_plugin/, scripts/, skills/, and the files in NEEDED. A
pull request that changes only files in SKIPPABLE, and no file in NEEDED, cannot change their result.

Usage: git diff --no-renames --name-only BASE HEAD | python3 scripts/ci_changes.py

The input is the changed file names, one on each line. The output is one line: `skip` or `run`. The rule fails safe: an
empty list, a name that the rule does not know, or a name in NEEDED gives `run`. With --no-renames, git writes a renamed
file as its old name and its new name, so the rule checks both.
"""
import sys

# Folders that no test of the service checks reads. Each name ends with a slash, so 'ios/' does not match 'iosx/'.
SKIPPABLE = (
    'ios/',  # The iPhone app. ios-tests.yml and mac-app.yml test it.
    'mac/',  # The Mac app. mac-app.yml tests it.
    'relay/',  # The relay worker. backend/tests uses a fake relay (team_fakes.py) and does not read relay/.
    'updates/',  # The files of the update feed. Only scripts/mac_feed.py reads them, and mac-app.yml tests it.
    '.github/workflows/',  # The other workflows. No test reads a workflow file.
)

# Files and folders inside SKIPPABLE that a test of the service checks reads, runs, or names.
NEEDED = (
    # This workflow runs the service checks, so a change of it must run them.
    '.github/workflows/hermes-compatibility.yml',
    # remote.source_files() packs ios/Tunnel/*.swift, and scripts/tunnel.py and scripts/team.py compile them.
    # test_tunnel_deploy.py, test_team_deploy.py, and test_coconut_skill.py call remote.source_files().
    'ios/Tunnel/',
    # test_native_pairing_redaction.py reads ios/Coconut/Resources/hermes-pairing-message.txt.
    'ios/Coconut/Resources/',
    # test_coconut_skill.py reads the command list of the coconut tool in mac/Sources/CoconutTool/main.swift.
    'mac/Sources/CoconutTool/',
)

# scripts/test_testflight.py reads the entitlements of the app and of the notification extension.
NEEDED_SUFFIXES = ('.entitlements',)


def needed(name):
    return name.startswith(NEEDED) or name.endswith(NEEDED_SUFFIXES)


def skippable(name):
    # git writes a name with unusual characters in quotes. Such a name, or a name with '..', is not known.
    parts = name.split('/')
    if not name or name.startswith(('"', '/')) or '..' in parts or '' in parts[:-1]:
        return False
    return name.startswith(SKIPPABLE) and not needed(name)


def decide(names):
    names = [name for name in names if name]
    return 'skip' if names and all(skippable(name) for name in names) else 'run'


def main():
    print(decide(line.rstrip('\r\n') for line in sys.stdin))


if __name__ == '__main__':
    main()
