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

The repository has no tests. The contract check at each service start is the compatibility check.

## Support a new Hermes release

1. Start Coconut on the release. `hermes_contract.verify_contract()` runs at the start.
2. If the contract check passes, add the release to `tested` and push to `main`.
3. If it fails, fix what the release breaks, then add the release to `tested`.
4. To stop the support of an old release, change `minimum` to a newer release in `tested`. Remove the older releases from `tested`.

Keep setup-specific update schedules, credentials, and installation procedures outside the repository.
