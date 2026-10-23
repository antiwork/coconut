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

## Checks of the installed Hermes

Coconut runs on the Hermes that the person installed, so it has no candidate runtime and no candidate gate.
The service runs the contract check against the installed Hermes at each start. An update runs it against the new code before it starts that code.
See [Hermes integration](hermes-integration.md#updates).
The compatibility CI still installs the revision in `scripts/hermes.py`. A later change replaces that pin with a range of supported versions.

After installation, repeat the installed contract and native checks to catch invalid paths or retained state.

Cache successful checks for unchanged hashes. Run them again after a source change, runtime change, or new failure.
Keep setup-specific update scheduling, credentials, and installation procedures outside the repository.
