# Supported Hermes versions

Coconut runs on the Hermes that the person installed. Coconut does not install Hermes and does not pin one Hermes version.

## The version file

`backend/coconut/hermes_versions.json` has two fields:

- `minimum` is the oldest Hermes release that local compatibility checks cover.
- `tested` lists the Hermes releases that passed local compatibility checks. It includes `minimum`.

The contract check decides whether Coconut runs. The service runs `hermes_contract.verify_contract()` against the installed Hermes at each start. When the check passes, Coconut runs, also on a version outside the tested range. When the check fails, Coconut starts in refuse mode.

The version file decides only whether Coconut reports the version as tested:

- A release from `minimum` to the newest `tested` release, inclusive, is tested.
- A newer release, an older release, a commit without a release version, or an unknown version is untested.
- `GET /v1/setup` reports `hermes.tested` as `true` or `false` when `hermes.status` is `ok`, and `null` otherwise.
- For an untested version, the startup log and `scripts/setup_coconut.py` print a warning with the newest tested version.

Before 0.21.6, Hermes used calendar versions, such as 2026.9.24. Coconut treats each calendar version as older than every 0.x release.

## Local compatibility checks

Regression checks run locally. The commit hook selects small service checks for staged changes. When the Hermes
contract changes, the agent also runs the contract and relevant native tests against the minimum and latest supported
Hermes releases. A broad compatibility change can require every backend test file, each in its own process.

GitHub runs only app release builds. There is no hosted compatibility matrix or nightly regression job.

## Run the native tests locally

The native tests use `backend/tests/hermes_env.py` to find Hermes:

1. Install Hermes with the official installer.
2. Set `COCONUT_HERMES_ROOT` to the Hermes source folder and `COCONUT_HERMES_PYTHON` to its Python. Without these variables, `hermes_env.py` uses the Hermes that `hermes_runtime` finds, but only when the tests run under the Python of that Hermes.
3. Run each test file with that Python, for example `PYTHONPATH=backend "$COCONUT_HERMES_PYTHON" -m unittest discover -s backend/tests -p test_native_kanban.py`.

Without a Hermes, the native tests skip. Set `COCONUT_REQUIRE_NATIVE=1` for a local compatibility check that must not skip. A missing Hermes then stops each native test module.

## Support a new Hermes release

1. Run the contract check and relevant native tests locally against the release.
2. If the checks pass, add the release to `tested` and commit to `main`.
3. If a check fails, fix what the release breaks, rerun it, then add the release to `tested`.
4. To stop the support of an old release, change `minimum` to a newer release in `tested`. Remove the older releases from `tested`.

These checks require no model credentials. The tests make no network calls. Cache successful contract checks for unchanged hashes. Run them again after a source change, a Hermes change, or a new failure.
Keep setup-specific update schedules, credentials, and installation procedures outside the repository.
