# Hermes upgrade checks

Every pull request runs the focused compatibility tests on Python 3.11.
These checks require no model credentials or network calls from the tests.
The workflow uses the official [checkout](https://github.com/actions/checkout) and [setup-python](https://github.com/actions/setup-python) actions.
Each test file runs in a separate process to prevent collisions between test module names.

Before an upgrade, also test the target runtime with isolated native profiles:

1. Run `python3 scripts/test_native_routines.py`.
2. Run `python3 scripts/test_script_routines.py`.
3. Run `python3 scripts/test_native_relay.py`.
4. Run `python3 scripts/test_native_profiles.py`.

These commands use the configured Mac mini and its managed Hermes installation.
The routine test checks ownership after a helper exits, concurrent claims, saved routes, and repeated completion.
It makes no model requests.
The relay test checks a busy recipient, independent work, pending decisions, replies, approval, and restart recovery.
The profile test checks native settings, approval recovery, and routine execution without repetition.
The latter two tests use model credentials already configured on the Mac.
The script test checks native output, silence, failure, cancellation, and completion without model access.
All four use disposable profiles. They do not use the user's bot conversations as fixtures.

## Candidate checks

Before you install a candidate runtime, run all four checks against it with one command:

```sh
python3 scripts/check_hermes_candidate.py --runtime /path/to/candidate --host-data ~/Coconut/.runtime/host
```

Run the command from the candidate Coconut checkout. The candidate folder holds the Hermes checkout in `source`.
The script finds `venv/bin/python` or `source/.venv/bin/python`. Use `--python` for another interpreter.

The script does these steps:

1. It records the Coconut commit, the Coconut tree, and the Hermes commit.
2. It stops if `backend` or `scripts` has uncommitted or untracked files, or if the Hermes checkout has changed or untracked files.
3. It builds the root layout that the programs expect in a temporary folder below `checks` in the candidate folder.
4. It runs the candidate contract, the concurrency check, and the Kanban check from `scripts/hermes.py check`.
5. It runs the routine, script routine, relay, and profile programs without changes.
6. It removes the temporary folder and checks that neither source changed.
7. It writes one JSON result. The default path is `checks/results/<source_sha>-<hermes_sha>.json` in the candidate folder.

The native profile of the checks stays in `terminal` in the candidate folder, because the model catalog keeps it beside the Hermes source.
Every temporary folder stays inside the candidate folder. The installed runtime and the user's bots stay untouched.
The checks inherit only the path, the home folder, the locale, and the TLS and proxy settings.
The script loads model keys with `host.load_credentials`. Only the relay and profile checks receive them.
The result names the loaded keys. The result and the logs never contain their values.
After each program, the script stops each process that still uses the temporary folder. Hermes workers run in their own session, so the process group does not include them.
If a contract program fails, the script stops. It does not start the other programs.

The result has a `passed` value and one entry for each check: `contracts`, `routines`, `script_routines`, `relay`, and `profiles`.
Each entry lists its programs with the exit code, the time, the result report, and the path of the full log.
The exit code is 0 only when every check passes.

### Native workers

Do not run the candidate checks from a native Kanban worker or from a process that the worker starts.
Hermes marks those processes with `HERMES_DELEGATED_CHILD_CONTEXT`. It then opens every board that `HERMES_KANBAN_DB` names as read-only.
Coconut sets that variable for each board that it opens. Thus the disposable boards of the checks cannot initialize.
The script finds the marker and stops before it runs a check. Do not remove the marker to continue.
Queue the command with a service that runs outside the worker. Then read the result file.

### Automatic updates

The automatic updater runs the candidate checks itself before it installs a new Hermes pin from `main`:

1. It recovers a pending update journal with `scripts/deploy.py recover`, which installs no package.
2. It needs 8 GiB of free disk space, model keys in the installed service, and unchanged files in `backend` and
   `scripts`. It checks these before the install, and it saves what is missing in `hermes-checks.json`.
3. It installs the pinned revision in its own checkout with `scripts/hermes.py install`.
4. It runs `scripts/check_hermes_candidate.py` against that runtime, with the model keys of the installed service.
   The result goes to `hermes-checks.json` next to the updater state.
5. It removes the folders that it wrote in `.runtime`.

The receipt has a `verdict`: `passed`, `failed` (the checks ran and a check failed), or `inconclusive` (a timeout,
missing model keys, changed code, or a setup error).

The live install starts only when the result passes for the pinned revision. The updater keeps each result for that
revision and the Git trees of `backend` and `scripts`, the code that the checks run. After a failure, it runs the
checks again after 24 hours, and at once for a new pin, for example a revert, or for a change of that code.
An inconclusive run waits 1, 2, 4, 8, and 16 hours, and then a day, before the next run for the same pin and code,
because each run installs the candidate and runs the checks again. A timeout, missing model keys, or a setup error of the checks is not a failure: the updater stops the
leftover check processes, and it runs the checks again after its normal backoff. A Coconut bot cannot run these checks inside its own Hermes worker,
so a pin pull request can merge on CI, Greptile, and review, and this gate protects the installation.

After installation, repeat the installed contract and native checks to catch invalid paths or retained state.

Cache successful checks for unchanged hashes. Run them again after a source change, runtime change, or new failure.
Keep setup-specific update scheduling, credentials, and installation procedures outside the repository.
