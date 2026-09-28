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

The automatic updater runs these checks itself before it installs a new Hermes pin from `main`.
First it recovers a pending update journal with `scripts/deploy.py recover`, which installs no package.
It needs 8 GiB of free disk space. Then it installs the pinned revision in its own checkout with
`scripts/hermes.py install`, and runs `scripts/hermes_candidate_checks.py --checkout <that checkout> --live <installed folder>`.
The live install starts only when every check passes. The updater saves the report in `hermes-checks.json` next
to its state, keeps the result for that revision and source commit, and removes its `.runtime` folder after the checks.
A failure stops the install. The updater runs the checks for the same pin and commit again after 24 hours,
and at once for a new commit on `main`. A Coconut bot cannot run these checks inside its own
Hermes worker, so a pin PR can merge on CI, Greptile, and review, and this gate protects the installation.

To test a candidate by hand, execute each script's `PROGRAM` with the candidate Python.
For the routine and relay programs, provide the runtime root and the candidate Coconut checkout as arguments.
For each entry in `test_native_profiles.PROGRAMS`, provide a root containing the candidate backend and `.runtime/hermes`.
Run the candidate contract first. Load model credentials privately when a check requires them.
Read each result and require its passing assertions. Keep evidence for both source and Hermes commit hashes.
After installation, repeat the installed contract and native checks to catch invalid paths or retained state.

Cache successful checks for unchanged hashes. Run them again after a source change, runtime change, or new failure.
Keep setup-specific update scheduling, credentials, and installation procedures outside the repository.
