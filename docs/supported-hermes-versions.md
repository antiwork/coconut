# Supported Hermes versions

Coconut runs on the Hermes that the person installed. Coconut does not install Hermes and does not pin one Hermes version.

## The version file

`backend/coconut/hermes_versions.json` has two fields:

- `minimum` is the oldest Hermes release that the compatibility CI checks.
- `tested` lists the Hermes releases that passed the compatibility CI. It includes `minimum`.

The contract check decides whether Coconut runs. The service runs `hermes_contract.verify_contract()` against the installed Hermes at each start. When the check passes, Coconut runs, also on a version outside the tested range. When the check fails, Coconut starts in refuse mode.

The version file decides only whether Coconut reports the version as tested:

- A release from `minimum` to the newest `tested` release, inclusive, is tested.
- A newer release, an older release, a commit without a release version, or an unknown version is untested.
- `GET /v1/setup` reports `hermes.tested` as `true` or `false` when `hermes.status` is `ok`, and `null` otherwise.
- For an untested version, the startup log and `scripts/setup_coconut.py` print a warning with the newest tested version.

Before 0.21.6, Hermes used calendar versions, such as 2026.9.24. Coconut treats each calendar version as older than every 0.x release.

## Compatibility CI

`.github/workflows/hermes-compatibility.yml` has these jobs:

- `contracts` runs every backend test file, each in its own process, on Python 3.11 without Hermes. The native tests skip there.
- `native-kanban (Hermes <minimum>)` checks the `minimum` release.
- `native-kanban (latest Hermes release)` checks the latest stable Hermes release.
- `native-kanban` passes only when both of the legs above pass.
- `native-kanban (Hermes main)` runs each night against the tip of Hermes `main`.

Each Hermes leg installs Hermes with the official installer, as a person does, into a temporary `HERMES_HOME`. Then Coconut finds that Hermes with `hermes_runtime.find()` and runs the contract check. Then the leg runs the native tests under the Python of that Hermes.

A failure of the nightly `main` leg is a warning in the run summary, not a failed check run. A change of Hermes `main` is not a fault of the Coconut commit.

On a pull request, the `changes` job first reads the changed files. `scripts/ci_changes.py` has the rule, and `scripts/test_ci_changes.py` tests it. When each changed file is an app file that no test of these jobs reads, `contracts` and the Hermes legs do not run. Then `contracts` reports a skipped job, and `native-kanban` passes. In all other cases, all checks run:

- A pull request that changes `backend/`, `hermes_plugin/`, `scripts/`, `skills/`, `docs/`, or a file outside the known app folders.
- A pull request that changes a file in an app folder that a test reads, such as `ios/Tunnel/`.
- A push to `main` and a manual run. The nightly run does not use the rule either.
- A failure of the `changes` job.

When a new test reads a file outside the service folders, add that file to `NEEDED` in `scripts/ci_changes.py`.

When the latest release is not in `tested`, its leg prints a warning. Add that release to `tested` when its leg passes.

## Run the native tests locally

The native tests use `backend/tests/hermes_env.py` to find Hermes:

1. Install Hermes with the official installer.
2. Set `COCONUT_HERMES_ROOT` to the Hermes source folder and `COCONUT_HERMES_PYTHON` to its Python. Without these variables, `hermes_env.py` uses the Hermes that `hermes_runtime` finds, but only when the tests run under the Python of that Hermes.
3. Run each test file with that Python, for example `PYTHONPATH=backend "$COCONUT_HERMES_PYTHON" -m unittest discover -s backend/tests -p test_native_kanban.py`.

Without a Hermes, the native tests skip. The native CI legs set `COCONUT_REQUIRE_NATIVE=1`. Then a missing Hermes stops each native test module, so no native test can skip there.

## Support a new Hermes release

1. Wait for the latest-release leg to run on a pull request or on `main`.
2. If the leg passes, add the release to `tested` in its own pull request.
3. If the leg fails, fix only what the new release breaks, in its own pull request. Then add the release to `tested`.
4. To stop the support of an old release, change `minimum` to a newer release in `tested`. Remove the older releases from `tested`.

These checks require no model credentials. The tests make no network calls. Cache successful contract checks for unchanged hashes. Run them again after a source change, a Hermes change, or a new failure.
Keep setup-specific update schedules, credentials, and installation procedures outside the repository.
